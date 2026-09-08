from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any, cast
from urllib.parse import quote, urljoin, urlparse

import httpx

from .author_privacy import AuthorHasher
from .config import Settings, get_settings
from .privacy import normalize_text
from .schemas import ContentType, SourceKind
from .sources import (
    CollectedItem,
    SourceBlockedError,
    SourceCallbacks,
    SourceCancelCallback,
    SourceCanceledError,
    SourceCheckpoint,
    SourceCollectionResult,
    SourceNotFoundError,
    SourceUnavailableError,
)

_PTT_BASE = "https://www.ptt.cc"
_DCARD_HOST = "www.dcard.tw"
_DCARD_PATH = re.compile(r"/f/(?P<forum>[A-Za-z0-9_-]+)/p/(?P<id>\d+)/?")


def _hash(*parts: object) -> str:
    rendered = "\0".join(normalize_text(str(part)) if part is not None else "" for part in parts)
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _is_new(item: CollectedItem, known_keys: set[str]) -> bool:
    return (
        item.source_item_id not in known_keys
        and item.content_hash not in known_keys
        and not any(alias in known_keys for alias in item.legacy_source_item_ids)
    )


def _parse_datetime(value: object) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC)
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(text)
        return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC)
    except (TypeError, ValueError):
        return None


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._href: str | None = None
        self._text: list[str] = []
        self.links: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href is not None:
            self.links.append((self._href, normalize_text("".join(self._text))))
            self._href = None
            self._text = []


def parse_ptt_search(html: str, board: str) -> tuple[list[str], str | None]:
    parser = _LinkParser()
    parser.feed(html)
    article_pattern = re.compile(rf"^/bbs/{re.escape(board)}/M\.[^/]+\.html$")
    articles: list[str] = []
    previous: str | None = None
    for href, text in parser.links:
        if article_pattern.fullmatch(href) and href not in articles:
            articles.append(href)
        # Only follow pagination on the same search endpoint, never an arbitrary
        # navigation link or a link to a different board.
        parts = urlparse(href)
        if (
            "上頁" in text
            and not parts.scheme
            and not parts.netloc
            and parts.path == f"/bbs/{board}/search"
        ):
            previous = parts.path + (f"?{parts.query}" if parts.query else "")
    return articles, previous


class _PttArticleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, str | None]] = []
        self.meta_values: list[str] = []
        self.metadata: dict[str, str] = {}
        self.body: list[str] = []
        self.pushes: list[dict[str, str]] = []
        self._meta: list[str] | None = None
        self._label: list[str] | None = None
        self._last_label = ""
        self._push: dict[str, list[str]] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        classes = set((values.get("class") or "").split())
        role: str | None = None
        if tag == "div" and values.get("id") == "main-content":
            role = "main"
        elif self._in("main") and tag == "div" and "push" in classes:
            role = "push"
            self._push = {"tag": [], "author": [], "text": [], "time": []}
        elif self._in("main") and classes.intersection(
            {"article-metaline", "article-metaline-right"}
        ):
            role = "ignore"
            self._last_label = ""
        elif self._in("main") and "article-meta-tag" in classes:
            role = "meta_label"
            self._label = []
        elif self._in("main") and "article-meta-value" in classes:
            role = "meta"
            self._meta = []
        elif self._in("push") and "push-tag" in classes:
            role = "push_tag"
        elif self._in("push") and "push-userid" in classes:
            role = "push_author"
        elif self._in("push") and "push-content" in classes:
            role = "push_text"
        elif self._in("push") and "push-ipdatetime" in classes:
            role = "push_time"
        elif tag in {"script", "style"}:
            role = "ignore"
        if tag in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            if tag in {"br", "hr"} and self._in("main") and not self._in("ignore"):
                if self._push is not None:
                    self._push["text"].append(" ")
                else:
                    self.body.append("\n")
            return
        self.stack.append((tag, role))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self._label is not None and self._nearest_role() == "meta_label":
            self._label.append(data)
            return
        if self._push is not None:
            role = self._nearest_role()
            mapping = {
                "push_tag": "tag",
                "push_author": "author",
                "push_text": "text",
                "push_time": "time",
            }
            if role in mapping:
                self._push[mapping[role]].append(data)
            return
        if self._meta is not None and self._nearest_role() == "meta":
            self._meta.append(data)
            return
        if self._in("main") and not self._in("ignore"):
            self.body.append(data)

    def handle_endtag(self, tag: str) -> None:
        index = next(
            (i for i in range(len(self.stack) - 1, -1, -1) if self.stack[i][0] == tag),
            None,
        )
        if index is None:
            return
        _tag, role = self.stack[index]
        del self.stack[index:]
        if role == "meta" and self._meta is not None:
            value = normalize_text("".join(self._meta))
            self.meta_values.append(value)
            if self._last_label:
                self.metadata[self._last_label] = value
            self._meta = None
        elif role == "meta_label" and self._label is not None:
            self._last_label = normalize_text("".join(self._label))
            self._label = None
        elif role == "push" and self._push is not None:
            self.pushes.append(
                {key: normalize_text("".join(value)) for key, value in self._push.items()}
            )
            self._push = None

    def _in(self, role: str) -> bool:
        return any(current == role for _tag, current in self.stack)

    def _nearest_role(self) -> str | None:
        return next((role for _tag, role in reversed(self.stack) if role), None)


def _clean_ptt_body(value: str) -> str:
    lines: list[str] = []
    for raw in value.replace("\r", "").splitlines():
        line = raw.strip()
        if line == "--":
            break
        if not line or line.startswith((">", "※ 發信站", "※ 文章網址", "※ 編輯", "※ 來自")):
            continue
        lines.append(line)
    return normalize_text("\n".join(lines))


_PTT_TZ = timezone(timedelta(hours=8))
_PTT_PUSH_TIME = re.compile(
    r"(?<!\d)(?P<month>\d{1,2})/(?P<day>\d{1,2})\s+"
    r"(?P<hour>\d{1,2}):(?P<minute>\d{2})(?!\d)"
)


def _ptt_article_datetime(value: str, thread_id: str) -> datetime | None:
    try:
        parsed = parsedate_to_datetime(value)
        return parsed.replace(tzinfo=parsed.tzinfo or _PTT_TZ).astimezone(UTC)
    except (TypeError, ValueError, OverflowError):
        # PTT's article ID embeds Unix time even when the metadata was removed.
        match = re.match(r"M\.(\d+)\.", thread_id)
        if match:
            try:
                return datetime.fromtimestamp(int(match.group(1)), UTC)
            except (ValueError, OverflowError, OSError):
                pass
        return None


def _ptt_push_datetime(value: str, article_date: datetime | None) -> datetime | None:
    match = _PTT_PUSH_TIME.search(value)
    if not match or article_date is None:
        return None
    values = {key: int(number) for key, number in match.groupdict().items()}
    article_date = article_date.replace(tzinfo=article_date.tzinfo or _PTT_TZ)
    local_article = article_date.astimezone(_PTT_TZ)
    now = datetime.now(UTC) + timedelta(days=1)
    # Push timestamps omit the year. Use the earliest plausible local year,
    # including New Year and leap-day transitions, and never invent invalid dates.
    for year in range(local_article.year, min(local_article.year + 8, now.year + 1) + 1):
        try:
            candidate = datetime(year, tzinfo=_PTT_TZ, **values).astimezone(UTC)
        except ValueError:
            continue
        if article_date - timedelta(minutes=1) <= candidate <= now:
            return candidate
    return None


def parse_ptt_article(
    html: str,
    source_url: str,
    hasher: AuthorHasher,
) -> list[CollectedItem]:
    parser = _PttArticleParser()
    parser.feed(html)
    match = re.search(r"/bbs/(?P<board>[^/]+)/(?P<id>M\.[^/]+)\.html", source_url)
    if not match:
        return []
    thread_id = match.group("id")
    raw_author = parser.metadata.get("作者", "")
    board = parser.metadata.get("看板") or match.group("board")
    title = parser.metadata.get("標題", "")
    raw_date = parser.metadata.get("時間", "")
    author = raw_author.split(" ", 1)[0]
    published_at = _ptt_article_datetime(raw_date, thread_id)
    body = _clean_ptt_body("".join(parser.body))
    if not body:
        return []
    post = CollectedItem(
        source="ptt",
        content_type="post",
        source_item_id=thread_id,
        thread_source_id=thread_id,
        content_hash=_hash("ptt", thread_id, body),
        author_hash=hasher.hash("ptt", author),
        title=title or None,
        board=board or None,
        text=body,
        published_at=published_at,
        date_precision="day" if published_at else "unknown",
        source_url=source_url,
    )
    items = [post]
    occurrences: dict[str, int] = {}
    for index, push in enumerate(parser.pushes, start=1):
        text = push["text"].lstrip(":： ")
        if not text or text in {"(本文已被刪除)", "[deleted]"}:
            continue
        signal = {"推": "push", "噓": "boo", "→": "neutral"}.get(
            push["tag"].strip(), "neutral"
        )
        comment_date = _ptt_push_datetime(push["time"], published_at)
        author_hash = hasher.hash("ptt", push["author"])
        time_match = _PTT_PUSH_TIME.search(push["time"])
        # push-ipdatetime can contain an IP address. Neither persist it nor let
        # changes to that metadata alter the identity of an existing comment.
        push_time = (
            f"{int(time_match['month']):02}/{int(time_match['day']):02} "
            f"{int(time_match['hour']):02}:{int(time_match['minute']):02}"
            if time_match else ""
        )
        identity = _hash("ptt-comment", thread_id, author_hash, signal, push_time, text)
        occurrences[identity] = occurrences.get(identity, 0) + 1
        source_id = _hash(identity, occurrences[identity])
        items.append(
            CollectedItem(
                source="ptt",
                content_type="comment",
                source_item_id=source_id,
                legacy_source_item_ids=[
                    _hash("ptt-comment", thread_id, index, signal, push["time"], text)
                ],
                thread_source_id=thread_id,
                parent_source_id=thread_id,
                content_hash=_hash("ptt", source_id, text),
                author_hash=author_hash,
                title=title or None,
                board=board or None,
                text=text,
                relative_date=push_time or None,
                published_at=comment_date,
                date_precision="day" if comment_date else "unknown",
                source_url=source_url,
                platform_data={"signal": signal, "floor": index},
            )
        )
    return items


class _DcardDocumentParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.scripts: list[tuple[str, str]] = []
        self.next_links: list[str] = []
        self._script_type: str | None = None
        self._script: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "meta":
            key = values.get("property") or values.get("name")
            content = values.get("content")
            if key and content:
                self.meta[key] = content
        elif tag == "script":
            self._script_type = values.get("type") or ""
            self._script = []
        elif tag in {"a", "link"} and "next" in (values.get("rel") or "").split():
            if values.get("href"):
                self.next_links.append(str(values["href"]))

    def handle_data(self, data: str) -> None:
        if self._script_type is not None:
            self._script.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._script_type is not None:
            self.scripts.append((self._script_type, "".join(self._script)))
            self._script_type = None
            self._script = []


def _walk_json(value: object) -> list[dict[str, Any]]:
    """Walk public embedded JSON without recursive-stack failures on nested payloads."""
    found: list[dict[str, Any]] = []
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, dict):
            found.append(current)
            pending.extend(reversed(list(current.values())))
        elif isinstance(current, list):
            pending.extend(reversed(current))
    return found


def _record_text(record: dict[str, Any]) -> str:
    # Metadata descriptions and search excerpts do not establish a full article body.
    for key in ("articleBody", "content", "text"):
        value = record.get(key)
        if isinstance(value, str) and normalize_text(value):
            return normalize_text(value)
    return ""


def _author_value(record: dict[str, Any]) -> str | None:
    value = record.get("author")
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("id", "name", "nickname"):
            if value.get(key):
                return str(value[key])
    for key in ("memberId", "authorId"):
        if record.get(key):
            return str(record[key])
    return None


def _dcard_number(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not re.fullmatch(r"\d{1,12}", text):
        return None
    return int(text)


def _dcard_datetime(value: object) -> datetime | None:
    from datetime import timezone

    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return _parse_datetime(value)
    return parsed.replace(tzinfo=parsed.tzinfo or timezone(timedelta(hours=8))).astimezone(UTC)


def _dcard_day(value: datetime) -> date:
    from datetime import timezone

    return value.astimezone(timezone(timedelta(hours=8))).date()


def _dcard_url(value: object, base: str = "https://www.dcard.tw") -> str | None:
    """Accept same-site article links and remove tracking from discovered anchors."""
    try:
        parsed = urlparse(urljoin(base, str(value)))
        if (
            parsed.scheme != "https"
            or (parsed.hostname or "").lower() != _DCARD_HOST
            or parsed.port not in {None, 443}
            or parsed.username is not None
            or parsed.password is not None
        ):
            return None
        match = _DCARD_PATH.fullmatch(parsed.path)
    except ValueError:
        return None
    if not match:
        return None
    post_id = match.group("id").lstrip("0") or "0"
    return f"https://{_DCARD_HOST}/f/{match.group('forum').lower()}/p/{post_id}"


def _dcard_record_id(record: dict[str, Any]) -> str | None:
    for key in ("id", "identifier", "url", "@id", "mainEntityOfPage"):
        value = record.get(key)
        if isinstance(value, dict):
            value = value.get("@id") or value.get("value") or value.get("url")
        if value is None or isinstance(value, (dict, list, bool)):
            continue
        rendered = str(value)
        if rendered.isdigit():
            return rendered.lstrip("0") or "0"
        canonical = _dcard_url(rendered)
        if canonical:
            return canonical.rsplit("/", 1)[-1]
    return None


def _dcard_is_comment(record: dict[str, Any]) -> bool:
    types = record.get("@type")
    return (
        "floor" in record
        or "commentId" in record
        or types == "Comment"
        or isinstance(types, list) and "Comment" in types
    )


def _dcard_comment_id(record: dict[str, Any]) -> str | None:
    value = record.get("commentId") or record.get("id")
    if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value).strip():
        return str(value).strip()
    return None


def _dcard_deleted(record: dict[str, Any]) -> bool:
    return bool(record.get("deletedAt")) or any(
        record.get(key) in (True, "true", 1) for key in ("deleted", "isDeleted")
    ) or _record_text(record) in {
        "[deleted]", "[removed]", "(本文已被刪除)", "此則留言已被刪除", "此留言已被刪除",
    }


def _dcard_nested_comments(value: object, post_id: str) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [record for item in value for record in _dcard_nested_comments(item, post_id)]
    if not isinstance(value, dict):
        return []
    if value.get("postId") is not None and str(value["postId"]) != post_id:
        return []
    found = [value] if _record_text(value) or _dcard_deleted(value) else []
    for key in ("comment", "comments", "replies"):
        found.extend(_dcard_nested_comments(value.get(key), post_id))
    return found


def _dcard_search_links(
    html: str, search_url: str, forums: set[str]
) -> tuple[list[str], str | None, bool]:
    from urllib.parse import parse_qs

    parser = _LinkParser()
    parser.feed(html)
    document = _DcardDocumentParser()
    document.feed(html)
    articles: list[str] = []
    seen_ids: set[str] = set()
    next_candidates = list(document.next_links)
    for href, text in parser.links:
        canonical = _dcard_url(href, search_url)
        if canonical:
            match = _DCARD_PATH.fullmatch(urlparse(canonical).path)
            assert match is not None
            post_id = match.group("id")
            if post_id not in seen_ids and (not forums or match.group("forum") in forums):
                articles.append(canonical)
                seen_ids.add(post_id)
        if text.lower() in {"next", "next page", "下一頁", "下頁", "›", "»"}:
            next_candidates.append(href)
    original_query = parse_qs(urlparse(search_url).query).get("query")
    original_forum = parse_qs(urlparse(search_url).query).get("forum")
    for href in next_candidates:
        candidate = urljoin(search_url, href)
        parsed = urlparse(candidate)
        try:
            allowed = (
                parsed.scheme == "https"
                and parsed.hostname == _DCARD_HOST
                and parsed.port in {None, 443}
                and parsed.username is None
                and parsed.password is None
                and parsed.path.rstrip("/") == "/search"
                and parse_qs(parsed.query).get("query") == original_query
                and parse_qs(parsed.query).get("forum") == original_forum
            )
        except ValueError:
            allowed = False
        if allowed:
            return articles, candidate, False
    exhausted = any(marker in html for marker in (
        "沒有搜尋結果", "找不到符合的結果", "沒有更多結果", "No results found", "No more results",
    ))
    return articles, None, exhausted


def parse_dcard_article(
    html: str,
    source_url: str,
    hasher: AuthorHasher,
) -> tuple[list[CollectedItem], bool]:
    canonical_url = _dcard_url(source_url)
    if canonical_url is None:
        return [], False
    source_url = canonical_url
    url_match = _DCARD_PATH.fullmatch(urlparse(source_url).path)
    if not url_match:
        return [], False
    forum = url_match.group("forum")
    post_id = url_match.group("id")
    parser = _DcardDocumentParser()
    parser.feed(html)
    records: list[dict[str, Any]] = []
    for _script_type, raw in parser.scripts:
        stripped = raw.strip()
        if not stripped or stripped[0] not in "[{":
            continue
        try:
            records.extend(_walk_json(json.loads(stripped)))
        except (json.JSONDecodeError, RecursionError):
            continue

    candidates = [record for record in records if (
        not _dcard_is_comment(record)
        and _dcard_record_id(record) == post_id
        and _record_text(record)
        and not _dcard_deleted(record)
    )]
    # The same article can appear as JSON-LD and hydration data. Prefer its full body.
    article_record = max(candidates, key=lambda record: len(_record_text(record)), default=None)
    if article_record is None:
        return [], False
    text = _record_text(article_record)
    raw_title = (
        (article_record or {}).get("title")
        or (article_record or {}).get("headline")
        or parser.meta.get("og:title")
    )
    title = normalize_text(str(raw_title)) if raw_title else ""
    if not text:
        return [], False
    published_at = _dcard_datetime(
        (article_record or {}).get("createdAt")
        or (article_record or {}).get("datePublished")
        or parser.meta.get("article:published_time")
    )
    items = [
        CollectedItem(
            source="dcard",
            content_type="post",
            source_item_id=post_id,
            thread_source_id=post_id,
            content_hash=_hash("dcard", post_id, text),
            author_hash=hasher.hash("dcard", _author_value(article_record or {})),
            title=title or None,
            board=forum,
            text=text,
            published_at=published_at,
            date_precision="day" if published_at else "unknown",
            source_url=source_url,
            platform_data={
                "reaction_count": _dcard_number(article_record.get("likeCount")) or 0,
            },
        )
    ]

    seen_comments: set[str] = set()
    loaded_comments: set[str] = set()
    nested_comments: set[int] = set()
    # Only comments within a matching article or explicitly referencing its ID qualify.
    for candidate in candidates:
        for key in ("comment", "comments", "replies"):
            for record in _dcard_nested_comments(candidate.get(key), post_id):
                nested_comments.add(id(record))
    declared_counts = [
        count for candidate in candidates
        if (count := _dcard_number(candidate.get("commentCount"))) is not None
    ]
    declared_comment_count = max(declared_counts, default=None)
    scoped_records: list[dict[str, Any]] = []
    for record in records:
        parent_post = record.get("postId")
        if parent_post is not None and str(parent_post) != post_id:
            continue
        is_nested = id(record) in nested_comments
        is_comment = is_nested or (_dcard_is_comment(record) and str(parent_post) == post_id)
        if not is_comment or any(record is candidate for candidate in candidates):
            continue
        scoped_records.append(record)
    # Stable platform IDs take precedence over the same comment's JSON-LD rendition.
    identified_fingerprints: set[str] = set()
    for record in sorted(scoped_records, key=lambda row: _dcard_comment_id(row) is None):
        comment_text = _record_text(record)
        raw_id = _dcard_comment_id(record)
        floor = _dcard_number(record.get("floor"))
        comment_date = _dcard_datetime(record.get("createdAt") or record.get("dateCreated") or record.get("datePublished"))
        author_hash = hasher.hash("dcard", _author_value(record))
        fingerprint = _hash("dcard-comment", post_id, floor, author_hash, comment_date, comment_text)
        if raw_id is None and fingerprint in identified_fingerprints:
            continue
        if raw_id:
            identified_fingerprints.add(fingerprint)
        comment_id = raw_id or fingerprint
        if comment_id == post_id or comment_id in seen_comments:
            continue
        if _dcard_deleted(record):
            loaded_comments.add(comment_id)
            continue
        if not comment_text:
            continue
        seen_comments.add(comment_id)
        loaded_comments.add(comment_id)
        items.append(
            CollectedItem(
                source="dcard",
                content_type="comment",
                source_item_id=comment_id,
                thread_source_id=post_id,
                parent_source_id=str(record.get("parentId") or post_id),
                content_hash=_hash("dcard", comment_id, comment_text),
                author_hash=author_hash,
                title=title or None,
                board=forum,
                text=comment_text,
                published_at=comment_date,
                date_precision="day" if comment_date else "unknown",
                source_url=source_url,
                platform_data={
                    "floor": floor,
                    "reaction_count": _dcard_number(record.get("likeCount")) or 0,
                },
            )
        )
    incomplete_marker = any(
        marker in html for marker in ("View more comments", "查看更多留言", "載入更多留言")
    )
    complete = not incomplete_marker and (
        declared_comment_count is not None and len(loaded_comments) >= declared_comment_count
    )
    items[0].platform_data.update({
        "declared_comment_count": declared_comment_count,
        "loaded_comment_count": len(loaded_comments),
        "public_page_complete": complete,
    })
    return items, complete


def dcard_import_item(record: dict[str, Any], hasher: AuthorHasher) -> CollectedItem:
    source_url = _dcard_url(record["source_url"])
    if source_url is None:
        raise ValueError("Dcard 匯入資料包含不允許的 URL")
    path_match = _DCARD_PATH.fullmatch(urlparse(source_url).path)
    if not path_match:
        raise ValueError("Dcard 匯入資料包含不允許的 URL")
    content_type = cast(ContentType, str(record["item_type"]))
    text = normalize_text(record["text"])
    thread_id = path_match.group("id")
    if record.get("thread_id") and str(record["thread_id"]) != thread_id:
        raise ValueError("Dcard 匯入 thread_id 與文章 URL 不一致")
    source_item_id = str(
        thread_id if content_type == "post" else record.get("source_item_id")
        or _hash("dcard-import", source_url, content_type, record.get("published_at"), text)
    )
    published_at = _dcard_datetime(record.get("published_at"))
    return CollectedItem(
        source="dcard",
        content_type=content_type,
        source_item_id=source_item_id,
        legacy_source_item_ids=[_hash(
            "dcard-import", str(record["source_url"]), content_type, record.get("published_at"), text
        )] if content_type == "post" else [],
        thread_source_id=thread_id,
        parent_source_id=str(record.get("parent_id") or thread_id)
        if content_type == "comment"
        else None,
        content_hash=_hash("dcard", source_item_id, text),
        author_hash=str(record.get("author_hash")) if record.get("author_hash") else None,
        title=normalize_text(str(record.get("title"))) if record.get("title") else None,
        board=(normalize_text(str(record.get("forum"))) if record.get("forum") else path_match.group("forum")),
        text=text,
        published_at=published_at,
        date_precision="day" if published_at else "unknown",
        source_url=source_url,
        platform_data={"reaction_count": _dcard_number(record.get("reaction_count")) or 0},
    )


@dataclass(slots=True)
class _HttpFetcher:
    settings: Settings
    interval: float
    allowed_hosts: set[str]
    client: httpx.AsyncClient | None = None
    _last_request: float = 0.0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def _validate_url(self, url: str, *, redirect: bool = False) -> None:
        try:
            parsed = urlparse(url)
            allowed = (
                parsed.scheme == "https"
                and parsed.port in {None, 443}
                and (parsed.hostname or "").lower() in self.allowed_hosts
                and parsed.username is None
                and parsed.password is None
            )
        except ValueError:
            allowed = False
        if not allowed:
            raise SourceBlockedError(
                "來源重新導向至未允許網域" if redirect else "來源網址不在允許清單"
            )
        if parsed.path.startswith("/ask/over18"):
            raise SourceBlockedError("PTT 限制級看板不在蒐集範圍")
        if parsed.path.rstrip("/") in {"/login", "/signup", "/auth/login"}:
            raise SourceBlockedError("來源要求登入，已停止公開頁蒐集")

    async def _wait(self, seconds: float, is_canceled: SourceCancelCallback | None) -> None:
        if is_canceled is None:
            await asyncio.sleep(seconds)
            return
        remaining = seconds
        while True:
            if await is_canceled():
                raise SourceCanceledError("任務已取消")
            if remaining <= 0:
                return
            step = min(remaining, 0.25)
            await asyncio.sleep(step)
            remaining -= step

    async def get(
        self, url: str, *, is_canceled: SourceCancelCallback | None = None
    ) -> str:
        self._validate_url(url)
        async with self._lock:
            return await self._get(url, is_canceled)

    async def _get(self, url: str, is_canceled: SourceCancelCallback | None) -> str:
        owns_client = self.client is None
        client = self.client or httpx.AsyncClient(
            timeout=self.settings.source_http_timeout_seconds,
            follow_redirects=False,
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
            headers={"User-Agent": "LocalReviewResearch/0.2 (+single-user; respectful-rate-limit)"},
        )
        try:
            current = url
            redirects = 0
            rate_limit_retried = False
            retries = 0
            while redirects <= 2:
                if is_canceled is not None and await is_canceled():
                    raise SourceCanceledError("任務已取消")
                delay = self.interval - (time.monotonic() - self._last_request)
                if delay > 0:
                    await self._wait(delay, is_canceled)
                try:
                    # Enforce redirect checks even with an injected client configured to follow.
                    response = await client.get(current, follow_redirects=False)
                except httpx.TransportError as exc:
                    if retries >= self.settings.source_http_retries:
                        raise SourceUnavailableError(
                            f"來源連線失敗：{type(exc).__name__}"
                        ) from exc
                    retries += 1
                    await self._wait(float(2 ** (retries - 1)), is_canceled)
                    continue
                finally:
                    self._last_request = time.monotonic()
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise SourceBlockedError("來源重新導向缺少目標")
                    target = urljoin(current, location)
                    self._validate_url(target, redirect=True)
                    current = target
                    redirects += 1
                    continue
                if response.status_code == 429 and not rate_limit_retried:
                    retry_after = _retry_after_seconds(response.headers.get("retry-after"))
                    if retry_after > 60:
                        raise SourceBlockedError("來源要求等待超過 60 秒，已停止本次蒐集")
                    await self._wait(retry_after, is_canceled)
                    rate_limit_retried = True
                    continue
                if response.status_code in {401, 403, 429}:
                    raise SourceBlockedError(f"來源拒絕請求（HTTP {response.status_code}）")
                if response.status_code in {404, 410}:
                    raise SourceNotFoundError(f"來源文章不存在或已刪除（HTTP {response.status_code}）")
                if response.status_code in {500, 502, 503, 504}:
                    if retries >= self.settings.source_http_retries:
                        raise SourceUnavailableError(f"來源暫時無法使用（HTTP {response.status_code}）")
                    retry_after = (
                        _retry_after_seconds(response.headers["retry-after"])
                        if "retry-after" in response.headers else float(2 ** retries)
                    )
                    if retry_after > 60:
                        raise SourceUnavailableError("來源要求等待超過 60 秒，已停止本次蒐集")
                    retries += 1
                    await self._wait(retry_after, is_canceled)
                    continue
                response.raise_for_status()
                body = response.text
                lowered = body.lower()
                # Do not treat a user quoting an error message inside an article as a block.
                heading = re.search(r"<(?:title|h1)[^>]*>(.*?)</(?:title|h1)>", lowered, re.S)
                challenge_text = heading.group(1) if heading else re.sub(r"<[^>]+>", " ", lowered).strip()[:300]
                if any(
                    marker in challenge_text
                    for marker in (
                        "access denied",
                        "service temporarily unavailable",
                        "just a moment...",
                        "verify you are human",
                        "系統負荷過重",
                    )
                ):
                    raise SourceBlockedError("來源回傳阻擋頁，已停止本次蒐集")
                return body
            raise SourceBlockedError("來源重新導向次數過多")
        except httpx.HTTPError as exc:
            raise SourceUnavailableError(f"來源連線失敗：{type(exc).__name__}") from exc
        finally:
            if owns_client:
                await client.aclose()


def _retry_after_seconds(value: str | None) -> float:
    if not value:
        return 1.0
    try:
        seconds = float(value)
        return max(seconds, 0.0) if math.isfinite(seconds) else 1.0
    except ValueError:
        try:
            target = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return 1.0
        target = target.replace(tzinfo=target.tzinfo or UTC).astimezone(UTC)
        return max((target - datetime.now(UTC)).total_seconds(), 0.0)


class PttSource:
    source: SourceKind = "ptt"

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.hasher = AuthorHasher(self.settings)
        self.fetcher = _HttpFetcher(
            self.settings,
            self.settings.ptt_request_interval_seconds,
            {"www.ptt.cc", "ptt.cc"},
            client,
        )

    async def collect(
        self,
        *,
        config: dict,
        checkpoint: SourceCheckpoint,
        callbacks: SourceCallbacks,
    ) -> SourceCollectionResult:
        known_keys = checkpoint.known_keys
        date_from = date.fromisoformat(config["date_from"])
        date_to = date.fromisoformat(config["date_to"])
        max_posts = int(config["max_posts"])
        max_comments = int(config["max_comments"])
        per_thread = int(config["max_comments_per_thread"])
        max_pages = max(1, min(int(config.get("max_search_pages", 20)), 100))
        posts, comments = checkpoint.post_count, checkpoint.comment_count
        seen_urls: set[str] = set()
        partial_reasons: set[str] = set()
        provider = checkpoint.provider
        config_hash = _hash(json.dumps({
            key: config.get(key) for key in (
                "boards", "keywords", "date_from", "date_to", "max_posts",
                "max_comments", "max_comments_per_thread", "max_search_pages",
            )
        }, sort_keys=True, default=str))
        same_config = provider.get("config_hash") == config_hash
        if same_config and "thread_comment_limit" in provider.get("partial_reasons", []):
            partial_reasons.add("thread_comment_limit")
        processed_urls = set(provider.get("processed_urls", [])) if same_config else set()
        thread_urls = list(dict.fromkeys(provider.get("thread_urls", [])))
        thread_counts = dict(provider.get("thread_comment_counts", {}))
        stats = {key: 0 for key in (
            "pages_fetched", "articles_fetched", "missing_articles", "unparsed_articles",
            "duplicate_articles", "filtered_articles", "truncated_threads",
        )}

        def limits_reached() -> bool:
            return posts >= max_posts and comments >= max_comments

        def provider_state() -> dict:
            return {
                "version": 1, "config_hash": config_hash, "thread_urls": thread_urls[:max_posts],
                "processed_urls": sorted(processed_urls), "thread_comment_counts": thread_counts,
                "partial_reasons": sorted(partial_reasons),
                **stats,
            }

        async def save_progress() -> None:
            # The manager persists this only after the corresponding batch is durable.
            await callbacks.on_metrics({"checkpoint": provider_state(), **stats})

        async def process_article(canonical: str) -> None:
            nonlocal posts, comments
            if await callbacks.is_canceled():
                raise SourceCanceledError("任務已取消")
            if canonical in seen_urls or canonical in processed_urls:
                stats["duplicate_articles"] += 1
                return
            seen_urls.add(canonical)
            match = re.fullmatch(r"/bbs/([^/]+)/(M\.[^/]+)\.html", urlparse(canonical).path)
            if not match or match.group(1) not in config["boards"]:
                return
            if posts >= max_posts and match.group(2) not in known_keys:
                return
            stats["articles_fetched"] += 1
            try:
                article_html = await self.fetcher.get(canonical, is_canceled=callbacks.is_canceled)
            except SourceNotFoundError:
                stats["missing_articles"] += 1
                partial_reasons.add("article_unavailable")
                await save_progress()
                return
            if await callbacks.is_canceled():
                raise SourceCanceledError("任務已取消")
            items = parse_ptt_article(article_html, canonical, self.hasher)
            if not items or items[0].published_at is None:
                stats["unparsed_articles"] += 1
                partial_reasons.add("article_parse_partial")
                await save_progress()
                return
            post = items[0]
            published_at = post.published_at
            assert published_at is not None
            if not date_from <= published_at.astimezone(_PTT_TZ).date() <= date_to:
                stats["filtered_articles"] += 1
                return
            new_posts = [post] if _is_new(post, known_keys) else []
            if new_posts and posts >= max_posts:
                return
            existing_comments = max(
                int(thread_counts.get(post.source_item_id, 0)),
                sum(not _is_new(item, known_keys) for item in items[1:]),
            )
            new_candidates = [item for item in items[1:] if _is_new(item, known_keys)]
            remaining_in_thread = max(per_thread - existing_comments, 0)
            eligible_comments = new_candidates[:remaining_in_thread]
            if max_comments and len(new_candidates) > remaining_in_thread:
                stats["truncated_threads"] += 1
                partial_reasons.add("thread_comment_limit")
            new_comments = eligible_comments[:max(max_comments - comments, 0)]
            if max_comments and len(new_comments) < len(eligible_comments):
                partial_reasons.add("comment_limit")
            new_items = [*new_posts, *new_comments]
            if new_items:
                if await callbacks.is_canceled():
                    raise SourceCanceledError("任務已取消")
                await callbacks.on_batch(new_items)
                known_keys.update(item.source_item_id for item in new_items)
                known_keys.update(item.content_hash for item in new_items)
            posts += len(new_posts)
            comments += len(new_comments)
            thread_counts[post.source_item_id] = existing_comments + len(new_comments)
            if canonical not in thread_urls:
                thread_urls.append(canonical)
            if len(new_comments) == len(eligible_comments):
                processed_urls.add(canonical)
            await save_progress()
            await callbacks.on_progress(
                posts, comments, max_posts + max_comments,
                f"PTT 已讀取 {posts} 篇文章、{comments} 則推文",
            )

        # Resume unfinished accepted threads directly, even when the post budget
        # is already consumed; do not open thousands of unrelated new articles.
        for canonical in list(thread_urls):
            if limits_reached():
                break
            await process_article(canonical)

        stop_search = limits_reached() or posts >= max_posts
        for board in dict.fromkeys(config["boards"]):
            if stop_search:
                break
            for keyword in dict.fromkeys(config["keywords"]):
                if stop_search:
                    break
                next_url: str | None = f"{_PTT_BASE}/bbs/{board}/search?q={quote(keyword, safe='')}"
                visited_pages: set[str] = set()
                for _page in range(max_pages):
                    if next_url is None:
                        break
                    if next_url in visited_pages:
                        partial_reasons.add("pagination_cycle")
                        break
                    if await callbacks.is_canceled():
                        raise SourceCanceledError("任務已取消")
                    visited_pages.add(next_url)
                    html = await self.fetcher.get(next_url, is_canceled=callbacks.is_canceled)
                    stats["pages_fetched"] += 1
                    links, previous = parse_ptt_search(html, board)
                    if not links and not re.search(
                        r'''\bclass\s*=\s*["'][^"']*\b(?:bbs-screen|r-list-container)\b''', html
                    ):
                        # A normal empty search retains PTT's layout; an unexpected
                        # document is not proof that there were no matching posts.
                        partial_reasons.add("article_parse_partial")
                    for href in links:
                        await process_article(urljoin(_PTT_BASE, href))
                        if posts >= max_posts:
                            stop_search = True
                            break
                    await save_progress()
                    if stop_search:
                        break
                    next_url = urljoin(_PTT_BASE, previous) if previous else None
                else:
                    if next_url:
                        partial_reasons.add(
                            "pagination_cycle" if next_url in visited_pages else "page_limit"
                        )

        reached = limits_reached()
        if reached:
            reason = "target_reached"
        else:
            if posts >= max_posts:
                partial_reasons.add("post_limit")
            reason = next((value for value in (
                "pagination_cycle", "page_limit", "article_parse_partial", "article_unavailable",
                "thread_comment_limit", "comment_limit", "post_limit",
            ) if value in partial_reasons), "search_exhausted")
        state = provider_state()
        state["partial_reasons"] = sorted(partial_reasons)
        return SourceCollectionResult(
            source="ptt", collected_count=posts + comments, post_count=posts,
            comment_count=comments, complete=reached or not partial_reasons,
            stop_reason=reason, checkpoint=state,
        )


class DcardSource:
    source: SourceKind = "dcard"

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.hasher = AuthorHasher(self.settings)
        self.fetcher = _HttpFetcher(
            self.settings,
            self.settings.dcard_request_interval_seconds,
            {_DCARD_HOST},
            client,
        )

    async def collect(
        self,
        *,
        config: dict,
        checkpoint: SourceCheckpoint,
        callbacks: SourceCallbacks,
    ) -> SourceCollectionResult:
        known_keys = set(checkpoint.known_keys)
        date_from = date.fromisoformat(str(config["date_from"]))
        date_to = date.fromisoformat(str(config["date_to"]))
        max_posts = int(config["max_posts"])
        max_comments = int(config["max_comments"])
        per_thread = int(config["max_comments_per_thread"])
        posts = checkpoint.post_count
        comments = checkpoint.comment_count
        thread_counts = {
            str(key): int(value)
            for key, value in checkpoint.provider.get("thread_comment_counts", {}).items()
        }
        thread_ids = set(str(value) for value in checkpoint.provider.get("thread_ids", []))
        known_keys.update(thread_ids)
        thread_urls = set(str(value) for value in checkpoint.provider.get("thread_urls", []))
        seen_threads: set[str] = set()
        partial_reasons: set[str] = set()
        limits = {"max_posts": max_posts, "max_comments": max_comments, "per_thread": per_thread}
        if checkpoint.provider.get("limits") == limits:
            partial_reasons.update(
                reason for reason in checkpoint.provider.get("partial_reasons", [])
                if reason in {"thread_comment_limit", "comment_limit"}
            )
        network_error: dict[str, str] = {}
        metrics = {"search_pages": 0, "article_requests": 0, "unavailable_articles": 0,
                   "duplicate_items": 0, "unknown_date_items": 0,
                   "comment_limit_reached": bool(partial_reasons)}

        def state() -> dict:
            return {
                **metrics,
                **network_error,
                "collection_scope": "public_html" if config.get("urls") or config.get("keywords") else "import_only",
                "thread_comment_counts": dict(thread_counts),
                "thread_ids": sorted(thread_ids),
                "thread_urls": sorted(thread_urls),
                "limits": limits,
                "partial_reasons": sorted(partial_reasons),
            }

        async def check_cancel() -> None:
            if await callbacks.is_canceled():
                raise SourceCanceledError("任務已取消")

        async def process_group(items: list[CollectedItem]) -> None:
            nonlocal posts, comments
            await check_cancel()
            if not items:
                return
            post = next((item for item in items if item.content_type == "post"), None)
            thread_id = items[0].thread_source_id or items[0].source_item_id
            if post:
                if post.published_at is None:
                    partial_reasons.add("unknown_date")
                    metrics["unknown_date_items"] += len(items)
                    return
                if not date_from <= _dcard_day(post.published_at) <= date_to:
                    return
                if _is_new(post, known_keys) and posts >= max_posts:
                    partial_reasons.add("post_limit")
                    return
            # Recover counts from old checkpoints when their per-thread map is absent.
            if thread_id not in thread_counts:
                thread_counts[thread_id] = len({
                    item.source_item_id for item in items
                    if item.content_type == "comment" and not _is_new(item, known_keys)
                })
            new_items: list[CollectedItem] = []
            batch_keys = set(known_keys)
            new_posts = 0
            new_comments = 0
            for item in ([post] if post else []) + [
                item for item in items if item.content_type == "comment"
            ]:
                await check_cancel()
                if not _is_new(item, batch_keys):
                    metrics["duplicate_items"] += 1
                    continue
                if post is None:
                    if item.published_at is None:
                        partial_reasons.add("unknown_date")
                        metrics["unknown_date_items"] += 1
                        continue
                    if not date_from <= _dcard_day(item.published_at) <= date_to:
                        continue
                if item.content_type == "comment":
                    if comments + new_comments >= max_comments:
                        if max_comments:
                            metrics["comment_limit_reached"] = True
                            partial_reasons.add("comment_limit")
                        continue
                    if thread_counts[thread_id] + new_comments >= per_thread:
                        metrics["comment_limit_reached"] = True
                        partial_reasons.add("thread_comment_limit")
                        continue
                    new_comments += 1
                else:
                    new_posts += 1
                new_items.append(item)
                batch_keys.update((item.source_item_id, item.content_hash))
            if new_items:
                await callbacks.on_batch(new_items)
                known_keys.update(batch_keys)
                posts += new_posts
                comments += new_comments
                thread_counts[thread_id] += new_comments
                thread_ids.add(thread_id)
                if post:
                    thread_urls.add(post.source_url)
                await callbacks.on_metrics({"checkpoint": state()})
            await callbacks.on_progress(
                posts, comments, max_posts + max_comments,
                f"Dcard 已讀取 {posts} 篇文章、{comments} 則留言",
            )

        async def fetch_public(url: str) -> str | None:
            await check_cancel()
            if network_error:
                return None
            try:
                return await self.fetcher.get(url, is_canceled=callbacks.is_canceled)
            except SourceNotFoundError:
                metrics["unavailable_articles"] += 1
                partial_reasons.add("source_unavailable")
            except SourceBlockedError as exc:
                network_error["blocked_reason"] = str(exc)
                partial_reasons.add("public_source_blocked")
            except SourceUnavailableError as exc:
                network_error["unavailable_reason"] = str(exc)
                partial_reasons.add("public_source_unavailable")
            await callbacks.on_metrics({"checkpoint": state()})
            return None

        async def process_url(raw_url: object) -> None:
            await check_cancel()
            url = _dcard_url(raw_url)
            if url is None:
                partial_reasons.add("invalid_url")
                return
            thread_id = url.rsplit("/", 1)[-1]
            if thread_id in seen_threads or network_error:
                return
            if posts >= max_posts and thread_id not in thread_ids and thread_id not in known_keys:
                partial_reasons.add("post_limit")
                return
            if thread_id in known_keys and (
                comments >= max_comments or thread_counts.get(thread_id, 0) >= per_thread
            ):
                return
            seen_threads.add(thread_id)
            metrics["article_requests"] += 1
            html = await fetch_public(url)
            if html is None:
                return
            items, page_complete = parse_dcard_article(html, url, self.hasher)
            if items and items[0].published_at and not date_from <= _dcard_day(items[0].published_at) <= date_to:
                return
            if not page_complete:
                partial_reasons.add("public_page_partial")
            await process_group(items)

        # Revisit accepted public threads before discovery when resuming at the post cap.
        if config.get("urls") or config.get("keywords"):
            for prior_url in sorted(thread_urls):
                await process_url(prior_url)
        # Explicit URLs are deliberate inputs and are independent of search forum filters.
        for raw_url in config.get("urls", []):
            await process_url(raw_url)

        # Only follow pagination rendered by the public site. Never infer API/page cursors.
        forums = {str(forum).lower() for forum in config.get("forums", [])}
        search_page_limit = int(config.get("max_search_pages", 5))
        for keyword in dict.fromkeys(config.get("keywords", [])):
            for forum in sorted(forums) if forums else [""]:
                if network_error:
                    break
                if posts >= max_posts:
                    partial_reasons.add("post_limit")
                    break
                first_url = f"https://{_DCARD_HOST}/search?query={quote(str(keyword), safe='')}"
                if forum:
                    first_url += f"&forum={quote(forum, safe='')}"
                next_url: str | None = first_url
                visited_pages: set[str] = set()
                for page in range(search_page_limit):
                    await check_cancel()
                    if not next_url or network_error:
                        break
                    if posts >= max_posts:
                        partial_reasons.add("post_limit")
                        break
                    if next_url in visited_pages:
                        partial_reasons.add("public_search_partial")
                        break
                    visited_pages.add(next_url)
                    search_url = next_url
                    metrics["search_pages"] += 1
                    html = await fetch_public(search_url)
                    if html is None:
                        break
                    urls, next_url, exhausted = _dcard_search_links(html, search_url, forums)
                    for url in urls:
                        await process_url(url)
                        if network_error:
                            break
                    if not next_url and not exhausted:
                        partial_reasons.add("public_search_partial")
                    if next_url and page + 1 >= search_page_limit:
                        partial_reasons.add("page_limit")

        # A blocked public endpoint must not prevent explicitly supplied offline imports.
        imported_by_thread: dict[str, list[CollectedItem]] = {}
        for record in config.get("import_records", []):
            await check_cancel()
            item = dcard_import_item(record, self.hasher)
            imported_by_thread.setdefault(item.thread_source_id or item.source_item_id, []).append(item)
        for items in imported_by_thread.values():
            await process_group(items)

        await check_cancel()
        reached = posts >= max_posts and comments >= max_comments
        if posts >= max_posts and not reached:
            partial_reasons.add("post_limit")
        if "blocked_reason" in network_error:
            stop_reason = "public_source_blocked"
        elif "unavailable_reason" in network_error:
            stop_reason = "public_source_unavailable"
        elif reached:
            stop_reason = "target_reached"
        elif "source_unavailable" in partial_reasons:
            stop_reason = "source_unavailable"
        elif "unknown_date" in partial_reasons:
            stop_reason = "unknown_date"
        elif "public_page_partial" in partial_reasons or "invalid_url" in partial_reasons:
            stop_reason = "public_page_partial"
        elif "page_limit" in partial_reasons:
            stop_reason = "page_limit"
        elif "public_search_partial" in partial_reasons:
            stop_reason = "public_search_partial"
        elif "thread_comment_limit" in partial_reasons:
            stop_reason = "thread_comment_limit"
        elif "comment_limit" in partial_reasons:
            stop_reason = "comment_limit"
        elif "post_limit" in partial_reasons:
            stop_reason = "post_limit"
        else:
            stop_reason = "input_exhausted"
        final_state = {**state(), "import_thread_count": len(imported_by_thread)}
        await callbacks.on_metrics({"checkpoint": final_state})
        return SourceCollectionResult(
            source="dcard",
            collected_count=posts + comments,
            post_count=posts,
            comment_count=comments,
            complete=(reached or not partial_reasons) and not network_error,
            stop_reason=stop_reason,
            checkpoint=final_state,
        )
